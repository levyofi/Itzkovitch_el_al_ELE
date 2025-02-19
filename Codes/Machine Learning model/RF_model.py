#%% Imports
import glob
import pandas as pd
import numpy as np
from osgeo import gdal
from sklearn.ensemble import RandomForestRegressor
from sklearn.metrics import r2_score
import pickle
import matplotlib.pyplot as plt
import os
import seaborn as sns
from pathlib import Path

#%% Data


'''
The input folder arranges as followed:

IR_fixed folder >> real thermal measurements files.
physical_model folder >> physical prediction files.
cropped_maps folder >> contain subfolders with flight's input data
'''

script_dir = Path(__file__).resolve()
pwd = script_dir.parents[2]
main_path = str(pwd) #input('Enter Folder Path for Maps:\n')
fold_name = 'Example data' #input('Enter Folder Name (to save data in):\n')
fold_path = f'{main_path}/{fold_name}'

#%% Model Class

def semi_random_sample(matrix_size, num_samples):
    submat_size = matrix_size // int(np.sqrt(num_samples))
    submat_steps = int(matrix_size / submat_size)
    
    points = []
    for i in range(submat_steps): 
        for j in range(submat_steps):
            x_start, x_end = i * submat_size, (i + 1) * submat_size
            y_start, y_end = j * submat_size, (j + 1) * submat_size
    
            x = np.random.randint(x_start, x_end)
            y = np.random.randint(y_start, y_end)
            
            points.append(int(x*matrix_size + y))
            
    while len(points) < 1000:
        x = np.random.randint(0, matrix_size)
        y = np.random.randint(0, matrix_size)
        points.append(int(x*matrix_size + y))
        
    return points

'''
We wrote the model as a python class, with the functions below:

init function - create dataframe with paths for each variable.
split data function - splitting data into train and test sets (based on prior classification).
trainRF - model training.
test - for each map in the test set, calculate the coeerction map and save it in npy format.
'''

class RF_Correction_Model():

    def __init__(self, path, name, map_size = 1_024):
        '''
        Create model class.
        At initiation - create dataframe that contain all paths to input maps
        '''

        self.name = name
        self.N = map_size
        self.features = ['TGI', 'height', 'shade', 'real_solar', 'skyview']

        files_dict = {'IR':[], 'TGI':[], 'height':[], 'shade':[], 'real_solar':[], 'skyview':[]}
        temp_list = glob.glob(f'{path}/Output data/*.tif')
        temp_list.sort()
        files_dict['M1'] = temp_list # before ML
        flights = [f.split('/')[-1][:-24] for f in temp_list] # from filelist, extract flight date and number
        self.unique_flights = [f.split('/')[-1][:-26] for f in temp_list[::5]] 
        files_dict['Flight'] = flights
        files_dict['MainFlight'] = [f.split('/')[-1][:-26] for f in temp_list]
        for name in self.unique_flights:
            for feature in self.features:
              tmp_list = glob.glob(f'{path}/Input data/cropped_{name}/{feature}_*.tif')
              tmp_list.sort()
              files_dict[feature] += tmp_list
            ir_list = glob.glob(f'{path}/Input data/cropped_{name}/*thermal_ir_*.tif')
            ir_list.sort()
            files_dict['IR'] += ir_list
        print({k:len(v) for k,v in files_dict.items()})

        self.files_df = pd.DataFrame(files_dict)

    def split_data(self, pixels, train_set_maps):
        '''
        Create dataframe for random forest model.
        For each map within the train set maps, and for each crop at those maps,
        the script takes [pixels] random pixels.
        '''
        self.train_flights = pd.Series(train_set_maps)
        self.len_of_random_pix = pixels

        masks = {}
        dctRF = {'PredM1': [], 'PredErrorM1':[],
               'TGI': [], 'Height': [], 'Shade': [], 'RealSolar': [], 'Skyview': [],
               }

        for flight in self.train_flights:
            subset = self.files_df[self.files_df['MainFlight'] == flight].reset_index()
            for crop in range(len(subset)):
                rand = semi_random_sample(self.N, pixels) # list of random indexes from the map
                m = np.zeros((self.N,self.N)).flatten()
                m[rand] = 1
                masks[f'{flight}_{crop}'] = m.reshape((self.N, self.N))

                dctRF['TGI'] += list(gdal.Open(subset['TGI'][crop]).ReadAsArray().flatten()[rand])
                dctRF['Height'] += list(gdal.Open(subset['height'][crop]).ReadAsArray().flatten()[rand])
                dctRF['Shade'] += list(gdal.Open(subset['shade'][crop]).ReadAsArray().flatten()[rand])
                dctRF['RealSolar'] += list(gdal.Open(subset['real_solar'][crop]).ReadAsArray().flatten()[rand])
                dctRF['Skyview'] += list(gdal.Open(subset['skyview'][crop]).ReadAsArray().flatten()[rand])

                pred_m1 = gdal.Open(subset['M1'][crop]).ReadAsArray().flatten()[rand]
                dctRF['PredM1'] += list(pred_m1)

                pred_error_m1 = gdal.Open(subset['M1'][crop]).ReadAsArray().flatten()[rand] - \
                    gdal.Open(subset['IR'][crop]).ReadAsArray().flatten()[rand] - 273.16
                dctRF['PredErrorM1'] += list(pred_error_m1 - np.nanmean(pred_error_m1)) # centralized to calculate the residuals

        train_df_rf = pd.DataFrame(dctRF)
        train_df_rf = train_df_rf.dropna()
        train_df_rf = train_df_rf.reset_index()
        self.RF_train_df = train_df_rf

        self.mask = masks

    def trainRF(self, plot = False):
        '''
        run basic random forest pipeline on the training data.
        '''
        X = self.RF_train_df.drop(['index', 'PredErrorM1'], axis = 1)
        y = self.RF_train_df['PredErrorM1']

        rf_model = RandomForestRegressor(random_state=42, n_estimators = 100, max_depth=10)
        rf_model.fit(X, y)
        y_pred = rf_model.predict(X)
        r2_RF = r2_score(y, y_pred)
        importance = rf_model.feature_importances_
        importance_df = pd.DataFrame({'feature': X.columns, 'coefficient': importance})
        self.RFModel = rf_model
        self.feature_importances = importance_df
        self.trainedR2_RF = r2_RF

        print(f'r^2:\tRF = {r2_RF:.3f}')

        if plot:
            rand = np.random.randint(0, len(y), 1000)
            sns.regplot(x = y[rand], y = y_pred[rand])
            plt.ylabel('Predicted')
            plt.xlabel('Real')
            plt.title('RF model')
            plt.show()

    def test(self, path):
        '''
        for maps in test set (not in train set), the function create dataframe from
        input maps and run the model.
        '''
        for flight in self.unique_flights:
            if flight in list(self.train_flights):
                continue
            subset = self.files_df[self.files_df['MainFlight'] == flight].reset_index()
            for crop, sub_flight in enumerate(subset['M1']):
                name = sub_flight.split('/')[-1][:-4]
                tgi = gdal.Open(subset['TGI'][crop]).ReadAsArray().flatten()
                height = gdal.Open(subset['height'][crop]).ReadAsArray().flatten()
                shade = gdal.Open(subset['shade'][crop]).ReadAsArray().flatten()
                real_solar = gdal.Open(subset['real_solar'][crop]).ReadAsArray().flatten()
                skyview = gdal.Open(subset['skyview'][crop]).ReadAsArray().flatten()
                pred_m1 = gdal.Open(subset['M1'][crop]).ReadAsArray().flatten()

                dataRF = pd.DataFrame({'PredM1':pred_m1,
                                     'TGI':tgi, 'Height':height,
                                     'Shade':shade, 'RealSolar':real_solar, 'Skyview':skyview})

                m2_map = self.RFModel.predict(dataRF).reshape((self.N, self.N))
                np.save(f'{path}/Output data/{name}_Correction_map.npy', m2_map)

#%% Run the model

# train the model
train_set = ["Zeelim_31.05.21_1516"]
model2 = RF_Correction_Model(main_path + '/Example data', 'afterML')
model2.split_data(1_000, train_set)
model2.trainRF(plot = True)

try:
    os.mkdir(fold_path)
except:
    pass

# run predictions on test set
model2.test(fold_path)

# save the model
with open(f'{fold_path}/after_ml.pkl', 'wb') as handle:
    pickle.dump(model2, handle, protocol=pickle.HIGHEST_PROTOCOL)

#%% Create summary DF for model performances

files = glob.glob(fold_path + '/*RF.npy')
d = {'Map':[],
     'M1_ME': [], 'M2_ME': [],
     'M1_MAE': [], 'M2_MAE': [],
     'M1_STD': [], 'M2_STD': [],
     'M1_MAD': [], 'M2_MAD': []
     }
for file in files:
    name = file.split('/')[-1][:-13]
    try:
      beforeML = glob.glob(f'{main_path}/{name}*')[0]
      afterML = glob.glob(f'{fold_path}/{name}_Model_RF.npy')[0]
      ir = glob.glob(f'{main_path}/IR_fixed/{name}*')[0]
      tgi = glob.glob(f'{main_path}/cropped_maps/{name[:-2]}/TGI_{name[-1]}.tif')[0]
    except:
      print(file)
      continue

    beforeML_map = gdal.Open(beforeML).ReadAsArray() # tf.imread(t1)
    afterML_map = np.load(afterML)
    ir_map = np.load(ir) + 273.16
    tgi_map = gdal.Open(tgi).ReadAsArray() # tf.imread(tgi)

    beforeML_map[tgi_map > 0.04] = np.nan
    afterML_map[tgi_map > 0.04] = np.nan
    ir_map[tgi_map > 0.04] = np.nan

    if np.nanmean(beforeML_map - ir_map) > 1_000:
        continue

    d['Map'].append(name)

    d['beforeML_ME'].append(np.nanmean(beforeML_map - ir_map))
    d['beforeML_MAE'].append(np.nanmean(abs(beforeML_map - ir_map)))
    d['beforeML_STD'].append(np.nanstd(beforeML_map - ir_map))
    d['beforeML_MAD'].append(np.nanstd(abs(beforeML_map - ir_map)))

    d['beforeML_ME'].append(np.nanmean((beforeML_map - afterML_map - ir_map)))
    d['beforeML_MAE'].append(np.nanmean(abs(beforeML_map - afterML_map - ir_map)))
    d['beforeML_STD'].append(np.nanstd((beforeML_map - afterML_map - ir_map)))
    d['beforeML_MAD'].append(np.nanstd(abs(beforeML_map - afterML_map - ir_map)))

df = pd.DataFrame(d)

# save df
df.to_csv(f'{fold_path}/Tables/correction_model_results_{fold_name}.csv')

#%% Summarize results

df['Flight'] = df["Map"].apply(lambda x: pd.Series(str(x)[:-2]))
df.groupby('Flight').mean()
