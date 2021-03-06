 #
# Copyright (c) 2019-2020, NVIDIA CORPORATION.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

# compute and algorithm defaults
default_model_type = 'XGBoost'
default_compute_type = 'sigle-GPU'
default_cv_folds = 1

# hyper-parameter defaults
default_max_depth = 5
default_n_estimators = 10

# directory defaults [ SageMaker specific ]
default_directory_structure = {
    'train_data' : '/opt/ml/input/data/training/', 
    'model_store' : '/opt/ml/model' 
}

# CPU imports
try:
    import dask, sklearn, pandas
    from dask.distributed import LocalCluster  
    from sklearn.metrics import accuracy_score as sklearn_accuracy_score    
except: pass

# GPU imports
try:
    import cudf, dask_cudf, cuml, cupy
    from dask_cuda import LocalCUDACluster    
    from cuml.metrics import accuracy_score as cuml_accuracy_score    
except: pass

# shared imports
import xgboost
import time, sys, os, argparse 
import glob, json, pprint, joblib
from dask.distributed import wait, Client
import warnings; warnings.filterwarnings("ignore")

# -------------------------------------------------------------------------------------------------------------
#  RapidsCloudML class
# -------------------------------------------------------------------------------------------------------------
class RapidsCloudML ( object ):
    """  Cloud integrated RAPIDS HPO functionality with AWS SageMaker focus """
    def __init__ ( self, input_args, 
                  dataset_path = '*.parquet', 
                  dataset_structure = default_directory_structure,
                  worker_limit = None ):

        # parse jobname to determine runtime configuration
        self.model_type, self.compute_type, self.cv_folds = parse_job_name()

        # parse input parameters for HPO        
        self.model_params = self.parse_hyper_parameter_inputs ( input_args )

        # data paths and datafile enumeration
        self.dataset_path = dataset_path
        self.dataset_structure = dataset_structure
        self.target_files, self.n_datafiles = self.configure_data_inputs()

        # determine the dask cluster size if appropriate
        if 'multi' in self.compute_type: 
            self.cluster, self.client = self.cluster_initialize( worker_limit = worker_limit )

        # variables for keeping track of metrics
        self.cv_fold_scores = []        
        self.best_score = -1 

    # -------------------------------------------------------------------------------------------------------------
    #  parse ML model parameters [ e.g., passed in by cloud HPO ]
    # -------------------------------------------------------------------------------------------------------------
    def parse_hyper_parameter_inputs ( self, input_args ):
        """ Parse hyperparmeters that are fed in by the HPO orchestrator [e.g., SageMaker ]. """
        print('parsing model hyper-parameters from command line arguments...\n')
        parser = argparse.ArgumentParser ()

        if 'XGBoost' in self.model_type:
            parser.add_argument( '--max_depth',       type = int,   default = default_max_depth )
            parser.add_argument( '--num_boost_round', type = int,   default = default_n_estimators )            
            parser.add_argument( '--subsample',       type = float, default = .25 )
            parser.add_argument( '--learning_rate',   type = float, default = 0.3 )            
            parser.add_argument( '--lambda_l2',       type = float, default = .2 )            
            parser.add_argument( '--gamma',           type = float, default = 0. )            
            parser.add_argument( '--alpha',           type = float, default = 0. )
            parser.add_argument( '--seed',            type = int,   default = 0 )
            
            args, unknown_args = parser.parse_known_args( input_args )
            
            model_params = {            
                'max_depth' : args.max_depth,
                'num_boost_round': args.num_boost_round,
                'learning_rate': args.learning_rate,
                'gamma': args.gamma,
                'lambda': args.lambda_l2,
                'random_state' : 0,
                'verbosity' : 0,
                'seed': args.seed,   
                'objective' : 'binary:logistic'
            }        

            if 'single-CPU' in self.compute_type:
                model_params.update( { 'nthreads': os.cpu_count() })

            if 'GPU' in self.compute_type:
                model_params.update( { 'tree_method': 'gpu_hist' })
            else:
                model_params.update( { 'tree_method': 'hist' })
            
        elif 'RandomForest' in self.model_type:
            parser.add_argument( '--max_depth',    type = int,   default = default_max_depth )
            parser.add_argument( '--n_estimators', type = int,   default = default_n_estimators )            
            parser.add_argument( '--max_features', type = float, default = .25 )
            parser.add_argument( '--seed',         type = int,   default = 0 )

            args, unknown_args = parser.parse_known_args( input_args )

            model_params = {            
                'max_depth' : args.max_depth,
                'n_estimators' : args.n_estimators,        
                'max_features': args.max_features,
                'seed' : args.seed,
            }
        else:
            raise Exception(f"!error: unknown model type {self.model_type}")

        pprint.pprint( model_params, indent = 5 ); print( '\n' )
        return model_params

    # -------------------------------------------------------------------------------------------------------------
    # determine datafiles to use based on directory and wildcard inputs 
    # -------------------------------------------------------------------------------------------------------------    
    def configure_data_inputs ( self ):
        """ Scan dataset to determine which files to ingest and modify path based on compute_type.
            This should help confirm that a correct AWS S3 bucket choice has been made.
            Notes: single-CPU pandas read_parquet needs a directory input
                   single-GPU cudf read_parquet needs a list of files
                   multi-CPU/GPU can accept a directory 
        """
        target_files = self.dataset_structure['train_data'] + str( self.dataset_path )
        n_datafiles = len( glob.glob(target_files) )
        assert( n_datafiles > 0 )

        if 'single-CPU' in self.compute_type:
            # pandas read_parquet needs a directory input
            target_files = target_files.split('*')[0]

        elif 'single-GPU' in self.compute_type:
            # cudf read_parquet needs a list of files
            target_files = glob.glob( target_files )
                    
        pprint.pprint( target_files ); print('\n')
        print( f'detected {n_datafiles} files as input \n')
        return target_files, n_datafiles

    # -------------------------------------------------------------------------------------------------------------
    # ETL 
    # -------------------------------------------------------------------------------------------------------------    
    def ETL ( self, columns = None, label_column = None, random_seed = 0 ):
        """ Perfom ETL given a set of target dataset to prepare for model training. 
            1. Ingest parquet compressed dataset
            2. Rebalance/Re-partition [ for multi-CPU and multi-GPU ]
            3. Drop samples with missing data [ predominantly cancelled flights ]
            4. Split dataset into train and test subsets 
        """
        with PerfTimer( 'ETL' ):            
            if 'single' in self.compute_type:
                if 'CPU' in self.compute_type:
                    from sklearn.model_selection import train_test_split
                    dataset = pandas.read_parquet( self.target_files, columns = columns, engine='pyarrow' )
                    dataset = dataset.dropna()
                    X_train, X_test, y_train, y_test = train_test_split( dataset.loc[:, dataset.columns != label_column], 
                                                                         dataset[label_column], random_state = random_seed )
                elif 'GPU' in self.compute_type:
                    from cuml.preprocessing.model_selection import train_test_split
                    dataset = cudf.read_parquet( self.target_files, columns = columns  )
                    dataset = dataset.dropna()
                    X_train, X_test, y_train, y_test = train_test_split( dataset, label_column, random_state = random_seed )
                
                return X_train, X_test, y_train, y_test

            elif 'multi' in self.compute_type:
                from dask_ml.model_selection import train_test_split
                if 'CPU' in self.compute_type:
                    dataset = dask.dataframe.read_parquet( self.target_files, columns = columns, engine='pyarrow') 
                elif 'GPU' in self.compute_type:
                    dataset = dask_cudf.read_parquet( self.target_files, columns = columns )
                
                # drop missing values [ ~2.5% -- predominantly cancelled flights ]
                dataset = dataset.dropna()

                # repartition [ inplace ], rebalance ratio of workers & data partitions
                initial_npartitions = dataset.npartitions    
                dataset = dataset.repartition( npartitions = self.n_workers )

                # split [ always runs, regardless of whether dataset is cached ]
                train, test = train_test_split( dataset, random_state = random_seed ) 

                # build X [ features ], y [ labels ] for the train and test subsets
                y_train = train[label_column].astype('int32')
                X_train = train.drop(label_column, axis = 1).astype('float32')

                y_test = test[label_column].astype('int32')
                X_test = test.drop(label_column, axis = 1).astype('float32')

                # return [ CPU/GPU ] dask dataframes 
                return X_train, X_test, y_train, y_test  
        
        return None

    # -------------------------------------------------------------------------------------------------------------
    # train
    # -------------------------------------------------------------------------------------------------------------
    def train_model ( self, X_train, y_train):
        """ Decision tree model training, architecture defined by HPO parameters. """
        with PerfTimer( f'training {self.model_type} classifier on {self.compute_type}'):
            
            if 'XGBoost' in self.model_type:
                
                if 'single' in self.compute_type:
                    dtrain = xgboost.DMatrix(data = X_train, label = y_train)
                    trained_model = xgboost.train( dtrain = dtrain, params = self.model_params, 
                                                   num_boost_round = self.model_params['num_boost_round'] )
                elif 'multi' in self.compute_type:
                    dtrain = xgboost.dask.DaskDMatrix( self.client, X_train, y_train)
                    xgboost_output = xgboost.dask.train( self.client, self.model_params, dtrain, 
                                                        num_boost_round = self.model_params['num_boost_round'] ) # evals=[(dtrain, 'train')]
                    trained_model = xgboost_output['booster']

            elif 'RandomForest' in self.model_type:

                if 'GPU' in self.compute_type:
                    if 'multi' in self.compute_type:
                        from cuml.dask.ensemble import RandomForestClassifier

                    elif 'single' in self.compute_type:
                        from cuml.ensemble import RandomForestClassifier

                    rf_model = RandomForestClassifier ( n_estimators = self.model_params['n_estimators'],
                                                        max_depth = self.model_params['max_depth'],
                                                        max_features = self.model_params['max_features'],
                                                        n_bins = 32 )
                elif 'CPU' in self.compute_type:
                    from sklearn.ensemble import RandomForestClassifier
                    rf_model = RandomForestClassifier ( n_estimators = self.model_params['n_estimators'],
                                                        max_depth = self.model_params['max_depth'],
                                                        max_features = self.model_params['max_features'], 
                                                        n_jobs=-1 )

                X_train, y_train = self.persist_training_inputs( X_train, y_train )
                trained_model = rf_model.fit( X_train.astype('float32'), y_train.astype('int32') )
                
        return trained_model

    def persist_training_inputs( self, X_train, y_train ):
        """ In the case of dask multi-CPU and dask multi-GPU Random Forest, 
            we need the dataset to be computed/persisted prior to a fit call.
            In the case of XGBoost this step is performed by the DMatrix creation.
        """
        if 'multi-CPU' in self.compute_type:
            X_train = X_train.persist()
            y_train = y_train.persist()

        elif 'multi-GPU' in self.compute_type:
            from cuml.dask.common.utils import persist_across_workers            
            X_train, y_train = persist_across_workers( self.client,
                                                        [ X_train, y_train ], 
                                                        workers = self.client.has_what().keys() )
            wait( [X_train, y_train ] )

        return X_train, y_train

    # -------------------------------------------------------------------------------------------------------------
    # predict / score
    # -------------------------------------------------------------------------------------------------------------
    def predict ( self, trained_model, X_test, y_test, threshold = 0.5 ):
        """ Inference with the trained model on the unseen test data """
        with PerfTimer(f'predict [ {self.model_type} ]'):
            
            if 'XGBoost' in self.model_type:              
                if 'single' in self.compute_type:  
                    dtest = xgboost.DMatrix( X_test, y_test)
                    predictions = trained_model.predict( dtest )
                    predictions = (predictions > threshold ) * 1.0

                elif 'multi' in self.compute_type:  
                    dtest = xgboost.dask.DaskDMatrix( self.client, X_test, y_test)
                    predictions = xgboost.dask.predict( self.client, trained_model, dtest).compute() 
                    predictions = (predictions > threshold ) * 1.0                    
                    y_test = y_test.compute()
                    
                if 'GPU' in self.compute_type:                
                    test_accuracy = cuml_accuracy_score ( y_test, predictions )
                elif 'CPU' in self.compute_type:
                    test_accuracy = sklearn_accuracy_score ( y_test, predictions )

            elif 'RandomForest' in self.model_type:
                if 'single' in self.compute_type:  
                    test_accuracy = trained_model.score( X_test, y_test )
                    
                elif 'multi' in self.compute_type:                    

                    if 'GPU' in self.compute_type:
                        y_test = y_test.compute()   
                        predictions = trained_model.predict( X_test ).compute()
                        test_accuracy = cuml_accuracy_score ( y_test, predictions )

                    elif 'CPU' in self.compute_type:
                        test_accuracy = sklearn_accuracy_score ( y_test, trained_model.predict( X_test ) )
            
            print(f'    subfold score: {test_accuracy}\n')
            self.cv_fold_scores += [ test_accuracy ]            
            return test_accuracy
    
    # -------------------------------------------------------------------------------------------------------------
    # emit score
    # -------------------------------------------------------------------------------------------------------------    
    def emit_final_score ( self ):
        """ Emit score for parsing by the cloud HPO orchestrator """
        if self.cv_folds > 1 :
            print(f'\t averaging over fold scores : {self.cv_fold_scores}')

        final_score = sum(self.cv_fold_scores) / len(self.cv_fold_scores) # average

        print(f'\t final-score: {final_score}; \n')
        
    # -------------------------------------------------------------------------------------------------------------
    # save model
    # -------------------------------------------------------------------------------------------------------------    
    def save_model ( self, model, output_filename='saved_model' ):
        """ Persist/save model.  For RandomForest follow sklearn best practices 
            https://scikit-learn.org/stable/modules/model_persistence.html
        """
        output_filename = self.dataset_structure['model_store'] + '/' + str( output_filename )

        with PerfTimer( f'saving model into {output_filename}' ):
            if 'XGBoost' in self.model_type:
                model.save_model( output_filename )
            elif 'RandomForest' in self.model_type:                
                joblib.dump ( model, output_filename )

    # -------------------------------------------------------------------------------------------------------------
    #  initialize and teardown compute cluster
    # -------------------------------------------------------------------------------------------------------------
    
    def cluster_reinitialize (self):
        """ Close the cluster/client in multi-CPU and mutli-GPU compute contexts.
            This gets called when multiple cross validation folds are used to prevent memory creep.
        """    
        print('\n! reinitializing cluster\n')
        if 'multi' in self.compute_type:
            self.client.close()
            self.cluster.close()
            self.cluster, self.client = self.cluster_initialize()
            
    def cluster_initialize (self, worker_limit=None):
        """ Initialize the dask compute cluster based on the number of available CPU/GPU workers.
            xgboost has a known issue where training fails if any worker has no data partition
            so when initializing a dask cluster for xgboost we may need to limit the number of workers
            see 3rd limitations bullet @ https://xgboost.readthedocs.io/en/latest/tutorials/dask.html 
        """
        with PerfTimer( f'create {self.compute_type} cluster'):

            cluster = None;  client = None

            # initialize CPU or GPU cluster
            if 'multi-GPU' in self.compute_type:

                self.n_workers = cupy.cuda.runtime.getDeviceCount()

                if 'XGBoost' in self.model_type:
                    self.n_workers = min( self.n_datafiles, self.n_workers ) 

                if worker_limit is not None:
                    self.n_workers = min( worker_limit, self.n_workers )
                                        
                cluster = LocalCUDACluster( n_workers = self.n_workers )
                client = Client( cluster )
                print(f'dask multi-GPU cluster with {self.n_workers} workers ')
                
            if 'multi-CPU' in self.compute_type:
                self.n_workers = os.cpu_count()

                if 'XGBoost' in self.model_type:
                    self.n_workers = min( self.n_datafiles, self.n_workers )
                    
                if worker_limit is not None:
                    self.n_workers = min( worker_limit, self.n_workers )
                
                cluster = LocalCluster(  n_workers = self.n_workers, threads_per_worker = 1 )
                client = Client( cluster )
                print(f'\ndask multi-CPU cluster with {self.n_workers} workers')

            return cluster, client

# -------------------------------------------------------------------------------------------------------------
# end of RapidsCloudML class 
# -------------------------------------------------------------------------------------------------------------

def parse_job_name():
    """ Unpack the string elements of the SageMaker job name to determine 
        compute and algorithm configuration settings. 
    """
    print('\nparsing compute & algorithm choices from job-name...\n')    
    model_type = default_model_type
    compute_type = default_compute_type
    cv_folds = default_cv_folds

    try:
        if 'SM_TRAINING_ENV' in os.environ:
            env_params = json.loads( os.environ['SM_TRAINING_ENV'] )
            job_name = env_params['job_name']

            # compute            
            compute_selection = job_name.split('-')[1].lower()
            if 'mgpu' in compute_selection:
                compute_type = 'multi-GPU'
            elif 'mcpu' in compute_selection:
                compute_type = 'multi-CPU'
            elif 'scpu' in compute_selection:
                compute_type = 'single-CPU'
            elif 'sgpu' in compute_selection:
                compute_type = 'single-GPU'

            # parse model type
            model_selection = job_name.split('-')[2].lower()
            if 'rf' in model_selection:
                model_type = 'RandomForest'
            elif 'xgb' in model_selection:
                model_type = 'XGBoost'
            
            # parse CV folds
            cv_folds = int(job_name.split('-')[3].split('cv')[0])
            
    except Exception as error:
        print( error )

    assert ( model_type in ['RandomForest', 'XGBoost'] )
    assert ( compute_type in ['single-GPU', 'multi-GPU', 'single-CPU', 'multi-CPU'] )
    assert ( cv_folds >= 1 )
    
    print(f'  compute: {compute_type}\n'
          f'  model: {model_type}\n'
          f'  cv_folds: {cv_folds}\n' )

    return model_type, compute_type, cv_folds


class PerfTimer:
    """ Performance timer [ uses highest system resolution time.perf_counter ]"""
    def __init__(self, name_string = '' ):
        self.start = None
        self.duration = None
        self.name_string = name_string

    def __enter__( self ):
        self.start = time.perf_counter()
        return self

    def __exit__( self, *args ):        
        self.duration = time.perf_counter() - self.start
        print(f"|-> {self.name_string} : {self.duration:.4f}\n")